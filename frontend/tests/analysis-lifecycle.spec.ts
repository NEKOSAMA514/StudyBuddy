import { taskResponse } from "./support/taskContract";
import { settingsRead, settingsValues } from "./support/settings";
import { test, expect, type Page, type Locator } from "@playwright/test";
import { readFileSync } from "node:fs";

test.beforeAll(async ({ request }) => {
  expect(
    (await (await request.get("/__test__/identity")).json()).fake_services_used,
  ).toBe(true);
});

async function mockWorkspace(page: Page) {
  const evidence = Array.from({ length: 36 }, (_, index) => ({
    id: `speech-${index}`,
    kind: "speech",
    start_seconds: index,
    end_seconds: index + 0.5,
    text: `转录 ${index}`,
    image_name: null,
    confidence: null,
  }));
  const frame = {
    ...evidence[0],
    id: "frame-1",
    kind: "frame",
    image_name: "frame.png",
    text: "画面",
  };
  const notes = {
    title: "完整笔记",
    tags: ["测试"],
    format_version: 4,
    overview_paragraphs: ["完整接口结果"],
    segment_guides: [] as any[],
    chapters: [{ chapter_id: "C1", title: "第一节", markdown: "要点", start_seconds: 5, end_seconds: 10,
      timestamp_basis: "speech", evidence_ids: [evidence[0].id, frame.id] }],
    source_issues: [] as any[],
    evidence_ids: [...evidence, frame].map((item) => item.id),
    model: "fake",
    prompt_version: "fake",
    coverage_note: "",
    basis: [],
  };
  const makeTask = (id: string, status = "succeeded") => ({
    id: `${id}-task`,
    video_id: id,
    created_at: "2026-10-13T00:00:00Z",
    updated_at: "2026-10-13T00:00:00Z",
    status,
    stage: status === "succeeded" ? "completed" : "ocr:1/10",
    config: {},
    warnings: [],
    timings: {},
    error: null as any,
    notes_state: status === "succeeded" ? "ready" : "none",
    notes_revision: status === "succeeded" ? 1 : 0,
  });
  const videos = ["first", "second"].map((id) => ({
    id,
    filename: `${id}.mp4`,
    pinned: false,
    duration: 120,
    size_bytes: 1024,
    created_at: "2026-10-14T00:00:00Z",
    media_info: { container: "mp4" } as any,
    latest_task: makeTask(id),
  }));
  const state = {
    videos,
    notes,
    noteResults: {} as Record<string, typeof notes>,
    evidenceByTask: {} as Record<string, typeof evidence>,
    evidence: [...evidence, frame],
    tasks: Object.fromEntries(
      videos.map((video) => [video.latest_task.id, video.latest_task]),
    ),
    reads: [] as string[],
    writes: [] as string[],
    unexpected: [] as string[],
    failPoll: false,
    delayFirst: false,
    release: () => {},
    slowReadStarted: false,
    healthReads: 0,
    modelConfigured: true,
  };
  let release!: () => void;
  const delayed = new Promise<void>((resolve) => {
    release = resolve;
  });
  state.release = release;
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    const method = request.method();
    if (method === "GET") state.reads.push(path);
    else state.writes.push(path);
    if (await settingsRead(route)) return;
    let body: unknown;
    if (path === "/api/v1/health") {
      state.healthReads++;
      body = {
        model_configured: state.modelConfigured,
        free_tier_confirmed: false,
        max_upload_bytes: 524288000,
        max_duration_seconds: 1200,
        supported_video_extensions: ["mp4"],
      };
    } else if (path === "/api/v1/videos") body = state.videos.map(video => ({ ...video, latest_task: video.latest_task ? taskResponse(video.latest_task) : null }));
    else if (path === "/api/v1/documents") body = [];
    else if (method === "GET" && /^\/api\/v1\/note-marks\/video\/[^/]+$/.test(path)) body = { marks: [] };
    else if (method === "GET" && /\/videos\/[^/]+\/analyses$/.test(path)) {
      const videoId = path.split("/").at(-2);
      body = Object.values(state.tasks).filter(task => task.video_id === videoId && task.status === "succeeded" && task.notes_state === "ready")
        .sort((a, b) => b.updated_at.localeCompare(a.updated_at) || b.id.localeCompare(a.id)).slice(0, 2).map(taskResponse);
    }
    else if (/\/videos\/[^/]+\/analysis-preview$/.test(path)) body = { character_count: null, estimated_segments: null, reason: "识别材料尚未就绪。" };
    else if (method === "PUT" && ["/api/v1/settings/model/profiles/default", "/api/v1/settings/recognition", "/api/v1/settings/network"].includes(path))
      body = settingsValues[path.replace("/api/v1", "").replace("/profiles/default", "/profiles")];
    else if (/\/tasks\/[^/]+\/(stop|resume|retry)$/.test(path)) {
      const [, id, action] = path.match(/\/tasks\/([^/]+)\/([^/]+)$/)!;
      state.tasks[id].status = action === "stop" ? "paused" : "queued";
      state.tasks[id].error = null;
      body = { task_id: id, status: state.tasks[id].status };
    } else if (/\/tasks\/[^/]+$/.test(path)) {
      if (state.failPoll) {
        state.failPoll = false;
        await route.fulfill({
          status: 503,
          json: { error: { message: "轮询失败", error_id: "poll-error" } },
        });
        return;
      }
      body = taskResponse(state.tasks[path.split("/").pop()!]);
    } else if (/\/analyses\/[^/]+$/.test(path)) {
      if (state.delayFirst && path.includes("first-task")) {
        state.slowReadStarted = true;
        await delayed;
      }
      body = state.noteResults[path.split("/").pop()!] || {
        ...state.notes,
        title: path.includes("first") ? "完整笔记 first" : "完整笔记 second",
      };
    } else if (path.endsWith("/evidence")) {
      if (state.delayFirst && path.includes("/first/")) {
        state.slowReadStarted = true;
        await delayed;
      }
      body = state.evidenceByTask[new URL(request.url()).searchParams.get("task_id") || ""] || state.evidence;
    } else if (path.endsWith("/playback"))
      body = {
        status: "unprepared",
        direct: true,
        mime_type: "video/mp4",
        original_url: path.replace(/\/playback$/, "/content"),
        url: null,
        error: null,
      };
    else if (path.endsWith("/content")) {
      const media = readFileSync(process.env.E2E_VIDEO_PATH!);
      const range = /bytes=(\d+)-(\d*)/.exec(request.headers().range || "");
      const start = range ? Number(range[1]) : 0;
      const end = range?.[2] ? Number(range[2]) : media.length - 1;
      await route.fulfill({
        status: range ? 206 : 200,
        contentType: "video/mp4",
        body: media.subarray(start, end + 1),
        headers: {
          "Accept-Ranges": "bytes",
          ...(range
            ? { "Content-Range": `bytes ${start}-${end}/${media.length}` }
            : {}),
        },
      });
      return;
    } else if (path.endsWith("/image")) {
      await route.fulfill({
        contentType: "image/png",
        body: Buffer.from(
          "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jZ1cAAAAASUVORK5CYII=",
          "base64",
        ),
      });
      return;
    } else {
      state.unexpected.push(`${method} ${path}`);
      await route.abort();
      return;
    }
    await route.fulfill({ json: body });
  });
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  (page as any).workspaceErrors = { state, errors };
  return state;
}

test("字幕沿用 speech 转录和依据，切换旧笔记恢复语音来源", async ({ page }) => {
  const state = await mockWorkspace(page);
  const source = { kind: "youtube_subtitle", language: "zh", automatic: true, digest: "1234567890abcdef" };
  Object.assign(state.tasks["first-task"].config, { transcript_source: source });
  Object.assign(state.notes, { recognition: { transcript_source: source } });
  state.tasks["old-task"] = { ...state.tasks["first-task"], id: "old-task", config: {}, updated_at: "2026-10-12T00:00:00Z" };
  state.noteResults["old-task"] = Object.assign({ ...state.notes }, { recognition: undefined });
  await page.goto("/#summary/first");
  await expect(page.getByRole("tab", { name: /平台自动字幕/ })).toBeVisible();
  await page.getByRole("button", { name: "查看本章依据" }).click();
  await expect(page.locator(".citation-table .evidence-type").filter({ hasText: "平台自动字幕" })).toHaveCount(1);
  const player = page.locator("video");
  await page.getByRole("button", { name: "回看 00:00 的平台自动字幕" }).click();
  await expect.poll(() => player.evaluate(video => (video as HTMLVideoElement).currentTime)).toBeLessThan(1);
  await page.getByRole("button", { name: "上一份笔记", exact: true }).click();
  await expect(page.getByRole("tab", { name: /语音转录/ })).toBeVisible();
  await expect(page.getByRole("tab", { name: /平台自动字幕/ })).toHaveCount(0);
  await expect(page.locator('.transcript-actions a').first()).toHaveAttribute("href", /task_id=old-task/);
});

test("关闭本次分析取消 API 配置加载，迟到失败不弹错", async ({ page }) => {
  await mockWorkspace(page);
  let release!: () => void;
  const held = new Promise<void>(resolve => { release = resolve; });
  let started = false;
  await page.route("**/api/v1/settings/model/profiles", async route => {
    started = true;
    await held;
    await route.fulfill({ status: 503, json: { error: { message: "迟到错误" } } }).catch(() => {});
  });
  await page.goto("/#summary/first");
  await page.getByRole("button", { name: "重新生成笔记", exact: true }).click();
  await expect.poll(() => started).toBe(true);
  await page.getByRole("button", { name: "关闭本次模型选择" }).click();
  release();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await expect(page.getByRole("alert")).toHaveCount(0);
});

test.afterEach(async ({ page }) => {
  const { state, errors } = (page as any).workspaceErrors;
  expect(state.unexpected).toEqual([]);
  expect(errors).toEqual([]);
});

test("正文失败后刷新仍显示大纲导览，未完成正文不请求或开放下载", async ({ page }) => {
  const state = await mockWorkspace(page);
  Object.assign(state.tasks["first-task"], { status: "failed", stage: "summarizing", notes_revision: 0,
    notes_state: "none", config: { outline_preview: "已保存的大纲材料导览。" } });
  await page.goto("/#summary/first");
  await expect(page.locator(".video-overview")).toContainText("已保存的大纲材料导览。");
  await expect(page.locator(".notes-downloads")).toHaveCount(0);
  await page.reload();
  await expect(page.locator(".video-overview")).toContainText("已保存的大纲材料导览。");
  expect(state.reads).not.toContain("/api/v1/analyses/first-task");
});

test("分段结果按版本追加，保留阅读位置和依据展开，最终补上概览", async ({ page }) => {
  const state = await mockWorkspace(page);
  const task = state.tasks["first-task"];
  state.videos[0].duration = 3600;
  const segment = { current: 2, completed: 1, total: 3, start_seconds: 1200, end_seconds: 2400, phase: "planning" };
  Object.assign(task, { status: "running", stage: "segments", notes_state: "partial", notes_revision: 1,
    progress: { percent: 66, completed_steps: 3, current_step: 4, processed: 1, total: 3, segmented: true, segment },
    segment_progress: segment });
  Object.assign(state.notes, { complete: false, covered_until_seconds: 1200, completed_segments: 1,
    total_segments: 3, overview_paragraphs: [] });
  state.notes.segment_guides.push({ index: 1, start_seconds: 0, end_seconds: 1200,
    first_chapter_id: "C1", summary: "第一段介绍核心概念和背景。" });
  state.notes.chapters[0].markdown = "第一段已经生成，可以边阅读边等待。\n\n".repeat(20);
  state.notes.chapters[0].start_seconds = 0;
  state.notes.chapters[0].end_seconds = 1200;
  await page.goto("/#summary/first");
  await expect(page.locator(".notes-partial")).toContainText("1/3");
  await expect(page.locator(".video-overview")).toHaveCount(0);
  await expect(page.getByRole("complementary", { name: "第1段导览" })).toContainText("第一段介绍核心概念和背景。");
  await expect(page.getByText("逐段生成笔记", { exact: false }).first()).toBeVisible();
  await page.locator("#video-notes").evaluate((element) => element.scrollIntoView({ block: "start" }));
  await page.screenshot({ path: `${process.env.E2E_REPORT_DIR}/partial-banner-${test.info().project.name}.png` });
  await page.locator("#note-block-C1 .cite-button").click();
  await page.locator("#note-block-C1").evaluate((element) => { element.dataset.preserved = "yes"; });
  const before = await page.evaluate(() => window.scrollY);
  state.notes.chapters.push({ ...state.notes.chapters[0], chapter_id: "C2", title: "第二段新增主题",
    markdown: "第二段只讲新增内容。", start_seconds: 1200, end_seconds: 2400 });
  state.notes.segment_guides.push({ index: 2, start_seconds: 1200, end_seconds: 2400,
    first_chapter_id: "C2", summary: "第二段补充应用方法和例外。" });
  Object.assign(state.notes, { covered_until_seconds: 2400, completed_segments: 2 });
  Object.assign(task, { notes_revision: 2 });
  await expect(page.locator("#note-block-C2")).toHaveCount(1);
  await expect(page.locator("#note-block-C1")).toHaveAttribute("data-preserved", "yes");
  await expect(page.locator("#note-block-C1 .cite-button")).toHaveAttribute("aria-expanded", "true");
  expect(Math.abs((await page.evaluate(() => window.scrollY)) - before)).toBeLessThan(3);
  await expect(page.locator(".notes-partial")).toContainText("2/3");
  await expect(page.locator(".segment-overview")).toHaveCount(2);
  expect(await page.locator(".timeline > *").evaluateAll((items) => items.map((item) => item.getAttribute("aria-label") || item.id)))
    .toEqual(["第1段导览", "note-block-C1", "第2段导览", "note-block-C2"]);
  await page.screenshot({ path: `${process.env.E2E_REPORT_DIR}/partial-${test.info().project.name}.png` });
  Object.assign(state.notes, { complete: true, overview_paragraphs: ["这是全视频的最终概览。"] });
  Object.assign(task, { status: "succeeded", stage: "completed", notes_state: "ready", notes_revision: 3 });
  await expect(page.locator(".video-overview")).toContainText("这是全视频的最终概览。");
  await expect(page.locator(".segment-overview")).toHaveCount(2);
  await expect(page.locator(".notes-partial")).toHaveCount(0);
  await expect(page.locator("#note-block-C1 .cite-button")).toHaveAttribute("aria-expanded", "true");
});

for (const status of ["paused", "failed"]) {
  test(`${status}任务刷新后仍可阅读和下载已完成片段`, async ({ page }) => {
    const state = await mockWorkspace(page);
    state.videos[0].duration = 3600;
    Object.assign(state.tasks["first-task"], { status, notes_state: "partial", notes_revision: 1 });
    Object.assign(state.notes, { complete: false, covered_until_seconds: 1200, completed_segments: 1,
      total_segments: 3, overview_paragraphs: [] });
    state.notes.segment_guides.push({ index: 1, start_seconds: 0, end_seconds: 1200,
      first_chapter_id: "C1", summary: "暂停或失败后保留第一段导览。" });
    await page.goto("/#summary/first");
    await expect(page.locator(".notes-partial")).toContainText("已完成内容可继续阅读和下载");
    await expect(page.locator("#note-block-C1")).toHaveCount(1);
    await expect(page.locator("#video-notes").getByRole("link", { name: "下载 TXT", exact: true }))
      .toHaveAttribute("href", "/api/v1/analyses/first-task/notes");
    await expect(page.locator("#video-notes").getByRole("link", { name: "下载 Markdown", exact: true }))
      .toHaveAttribute("href", "/api/v1/analyses/first-task/notes?format=md");
    await page.reload();
    await expect(page.locator("#note-block-C1")).toHaveCount(1);
    await expect(page.locator(".segment-overview")).toContainText("暂停或失败后保留第一段导览。");
    await expect(page.locator(".notes-partial")).toContainText("1/3");
  });
}

for (const regenerate of [false, true]) {
  test(`${regenerate ? "重新生成" : "生成笔记"}后两个文件列表立即按使用顺序排列并保留置顶`, async ({ page }) => {
    const state = await mockWorkspace(page);
    state.videos[0].created_at = "2026-10-10T00:00:00Z";
    state.videos[0].latest_task.created_at = "2026-10-11T00:00:00Z";
    if (!regenerate) state.videos[0].latest_task = null as any;
    const pinned = {
      ...state.videos[1], id: "pinned", filename: "pinned.mp4", pinned: true,
      latest_task: { ...state.tasks["second-task"], id: "pinned-task", video_id: "pinned" },
    };
    state.videos.push(pinned);
    state.tasks[pinned.latest_task.id] = pinned.latest_task;
    const generated = {
      ...state.tasks["first-task"], id: "generated-task", status: "running", stage: "ocr:1/10",
      created_at: "2026-10-15T00:00:00Z", updated_at: "2026-10-15T00:00:00Z", notes_state: "none", notes_revision: 0,
    };
    state.tasks[generated.id] = generated;
    await page.route("**/api/v1/videos/first/analyses", async (route) => {
      if (route.request().method() === "GET") return route.fallback();
      state.writes.push(new URL(route.request().url()).pathname);
      // 列表接口暂时返回旧快照，详情刚取得的新任务仍应立即决定排序。
      await route.fulfill({ status: 202, json: { task_id: generated.id, status: generated.status } });
    });
    await page.clock.install({ time: new Date("2026-10-15T00:00:00Z") });
    await page.clock.pauseAt(new Date("2026-10-15T00:00:01Z"));
    await page.goto("/#summary");
    const names = page.locator(".file-list .file-name");
    await expect(names).toHaveText(["置顶pinned.mp4", "second.mp4", "first.mp4"]);
    await names.filter({ hasText: "first.mp4" }).click();
    await page.getByRole("button", { name: regenerate ? "重新生成笔记" : "开始处理", exact: true }).click();
    await page.getByRole("dialog", { name: "选择处理模型" }).getByRole("button", { name: "开始处理", exact: true }).click();
    await expect(page.getByRole("dialog")).toHaveCount(0);
    await expect(page.locator(".sidebar .library-item")).toHaveText([
      /pinned\.mp4/, /first\.mp4/, /second\.mp4/,
    ]);
    generated.stage = "ocr:2/10";
    await page.clock.runFor(2000);
    await expect(page.locator(".task-activity")).toContainText("2/10");
    state.videos[0].latest_task = generated;
    await page.locator(".detail-back").click();
    await expect(names).toHaveText(["置顶pinned.mp4", "first.mp4", "second.mp4"]);
    await page.reload();
    await expect(names).toHaveText(["置顶pinned.mp4", "first.mp4", "second.mp4"]);
    await expect(page.locator(".sidebar .library-item")).toHaveText([
      /pinned\.mp4/, /first\.mp4/, /second\.mp4/,
    ]);
    expect(state.writes).toEqual(["/api/v1/videos/first/analyses"]);
  });
}

test("重新生成完成后自动替换旧笔记、引用与下载任务", async ({ page }) => {
  const state = await mockWorkspace(page);
  const regenerated = {
    ...state.tasks["first-task"],
    id: "first-regenerated-task",
    status: "running",
    stage: "finalizing",
    notes_state: "none", notes_revision: 0,
  };
  state.tasks[regenerated.id] = regenerated;
  await page.route("**/api/v1/videos/first/analyses", async (route) => {
    if (route.request().method() === "GET") return route.fallback();
    state.writes.push(new URL(route.request().url()).pathname);
    state.videos[0].latest_task = regenerated;
    await route.fulfill({ status: 202, json: { task_id: regenerated.id, status: regenerated.status } });
  });
  await page.route("**/api/v1/analyses/first-regenerated-task", (route) =>
    route.fulfill({ json: { ...state.notes, title: "重新生成的新笔记" } }),
  );
  await page.clock.install({ time: new Date("2026-10-15T00:00:00Z") });
  await page.clock.pauseAt(new Date("2026-10-15T00:00:01Z"));
  await page.goto("/#summary/first");
  await expect(page.locator(".overview h3")).toHaveText("完整笔记 first");
  await page.getByRole("button", { name: "查看本章依据", exact: true }).click();
  await page.getByRole("button", { name: "重新生成笔记", exact: true }).click();
  await page.getByRole("dialog", { name: "选择处理模型" }).getByRole("button", { name: "开始处理", exact: true }).click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await expect(page.locator(".summary-panel")).toHaveCount(1);
  await expect(page.locator(".overview h3")).toHaveText("完整笔记 first");
  regenerated.status = "succeeded";
  regenerated.notes_state = "ready";
  regenerated.notes_revision = 1;
  regenerated.stage = "completed";
  regenerated.updated_at = "2026-10-15T00:00:02Z";

  await page.clock.runFor(2000);
  await expect(page.locator(".task-panel .status-pill")).toHaveText("已完成");
  await expect(page.locator(".summary-panel")).toHaveCount(1);
  await expect(page.locator(".overview h3")).toHaveText("重新生成的新笔记");
  await expect(page.locator(".evidence-panel")).toHaveCount(1);
  await expect(page.locator("#block-evidence-C1")).toHaveCount(0);
  await expect(page.locator("#video-notes").getByRole("link", { name: "下载 TXT", exact: true })).toHaveAttribute("href", "/api/v1/analyses/first-regenerated-task/notes");
  await expect(page.locator(".transcript-actions a").last()).toHaveAttribute("href", /task_id=first-regenerated-task&format=json$/);
  expect(state.writes.filter((path) => path.endsWith("/analyses"))).toEqual(["/api/v1/videos/first/analyses"]);
});

function previousVersion(state: Awaited<ReturnType<typeof mockWorkspace>>) {
  const previous = { ...state.tasks["first-task"], id: "first-previous", updated_at: "2026-10-12T00:00:00Z" };
  state.tasks[previous.id] = previous;
  const source = { ...state.evidence[0], id: "previous-asr", text: "上一份识别配置的原文", start_seconds: 0.5 };
  state.evidenceByTask[previous.id] = [source];
  state.noteResults[previous.id] = { ...state.notes, title: "上一份成功笔记", evidence_ids: [source.id],
    chapters: [{ ...state.notes.chapters[0], evidence_ids: [source.id] }] };
  return previous;
}

test("版本旁快捷按钮按指定顺序切换与回到生成，右下角可回到顶端", async ({ page }) => {
  const state = await mockWorkspace(page);
  previousVersion(state);
  const current = { ...state.tasks["first-task"], id: "first-current", status: "paused", stage: "summarizing",
    notes_state: "none", notes_revision: 0, config: { outline_preview: "本次生成的材料导览" } };
  state.tasks[current.id] = state.videos[0].latest_task = current;
  await page.goto("/#summary/first");
  await expect(page.locator(".overview h3")).toHaveText("完整笔记 first");
  const controls = page.getByRole("group", { name: "笔记快捷操作" });
  await expect(page.locator("#video-notes select")).toHaveCount(0);
  await expect(controls.getByRole("button")).toHaveText(["最新笔记", "上一份笔记", "回到生成"]);
  await controls.getByRole("button", { name: "上一份笔记", exact: true }).click();
  await expect(controls.getByRole("button", { name: "上一份笔记", exact: true })).toHaveAttribute("aria-pressed", "true");
  await expect(page.locator(".overview h3")).toHaveText("上一份成功笔记");
  await controls.getByRole("button", { name: "最新笔记", exact: true }).click();
  await expect(controls.getByRole("button", { name: "最新笔记", exact: true })).toHaveAttribute("aria-pressed", "true");
  await expect(page.locator(".overview h3")).toHaveText("完整笔记 first");
  for (const width of [598, 390]) {
    await page.setViewportSize({ width, height: 871 });
    await page.locator(".note-version-controls").scrollIntoViewIfNeeded();
    const boxes = await controls.getByRole("button").all();
    const positions = await Promise.all(boxes.map(button => button.boundingBox()));
    expect(positions.every(box => box && Math.abs(box.y - positions[0]!.y) < 1)).toBe(true);
    if (width === 598) {
      const label = (await page.locator(".note-version").boundingBox())!;
      expect(Math.abs(label.y + label.height / 2 - positions[0]!.y - positions[0]!.height / 2)).toBeLessThan(1);
    }
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    for (const theme of ["dark", "light"]) {
      await page.getByRole("button", { name: theme === "dark" ? "切换到暗色模式" : "切换到亮色模式", exact: true }).click();
      await page.locator(".note-version-controls").screenshot({ path: `${process.env.E2E_REPORT_DIR}/note-shortcuts-${width}-${theme}-${test.info().project.name}.png` });
    }
  }
  await controls.getByRole("button", { name: "回到生成", exact: true }).click();
  await expect(controls.getByRole("button", { name: "最新笔记", exact: true })).toHaveAttribute("aria-pressed", "false");
  await expect(controls.getByRole("button", { name: "上一份笔记", exact: true })).toHaveAttribute("aria-pressed", "false");
  await expect(page.locator(".video-overview")).toContainText("本次生成的材料导览");
  await expect(page.locator("#generation-progress")).toBeFocused();
  expect(state.writes).toEqual([]);
  await page.locator(".evidence-panel").scrollIntoViewIfNeeded();
  await expect.poll(() => page.evaluate(() => window.scrollY)).toBeGreaterThan(100);
  const top = page.getByRole("button", { name: "回到顶端", exact: true });
  const box = (await top.boundingBox())!;
  expect(box.x + box.width).toBeLessThanOrEqual(390);
  expect(871 - box.y - box.height).toBe(20);
  await top.click();
  await expect.poll(() => page.evaluate(() => window.scrollY)).toBe(0);
});

test("切换上一份笔记同时切换下载、识别转录和回看依据", async ({ page }) => {
  const state = await mockWorkspace(page);
  const previous = previousVersion(state);
  await page.goto("/#summary/first");
  await expect(page.locator(".overview h3")).toHaveText("完整笔记 first");
  await page.getByRole("button", { name: "上一份笔记", exact: true }).click();
  await expect(page.locator(".overview h3")).toHaveText("上一份成功笔记");
  await page.locator("#video-notes").screenshot({ path: `${process.env.E2E_REPORT_DIR}/note-version-${test.info().project.name}.png` });
  await expect(page.locator("#video-notes .notes-download").first()).toHaveAttribute("href", `/api/v1/analyses/${previous.id}/notes`);
  await expect(page.locator("#video-notes .notes-download").last()).toHaveAttribute("href", `/api/v1/analyses/${previous.id}/notes?format=md`);
  await expect(page.locator(".transcript-actions a").first()).toHaveAttribute("href", `/api/v1/videos/first/transcript?task_id=${previous.id}&format=txt`);
  await page.getByRole("button", { name: "查看本章依据", exact: true }).click();
  await expect(page.locator(".citation-table")).toContainText("上一份识别配置的原文");
  await expect(page.locator(".citation-table")).not.toContainText("转录 0");
  await page.locator("video").evaluate(async (video: HTMLVideoElement) => {
    if (video.readyState < 1) await new Promise(resolve => video.addEventListener("loadedmetadata", resolve, { once: true }));
  });
  await page.locator(".citation-table .time-button").click();
  await expect.poll(() => page.locator("video").evaluate((video: HTMLVideoElement) => video.currentTime)).toBeCloseTo(0.5, 1);
  await page.getByRole("button", { name: "最新笔记", exact: true }).click();
  await expect(page.locator(".overview h3")).toHaveText("完整笔记 first");
  await expect(page.locator("#block-evidence-C1")).toHaveCount(0);
});

test("没有手动选择旧版本时跟随新草稿及成功结果", async ({ page }) => {
  const state = await mockWorkspace(page);
  previousVersion(state);
  const current = { ...state.tasks["first-task"], id: "first-follow", status: "running", stage: "segments", notes_state: "none", notes_revision: 0 };
  state.tasks[current.id] = state.videos[0].latest_task = current;
  state.noteResults[current.id] = { ...state.notes, title: "自动跟随的草稿" };
  await page.clock.install({ time: new Date("2026-10-15T00:00:00Z") });
  await page.clock.pauseAt(new Date("2026-10-15T00:00:01Z"));
  await page.goto("/#summary/first");
  await expect(page.locator(".overview h3")).toHaveText("完整笔记 first");
  Object.assign(current, { notes_state: "partial", notes_revision: 1 });
  await page.clock.runFor(500);
  await expect(page.locator(".overview h3")).toHaveText("自动跟随的草稿");
  Object.assign(current, { status: "succeeded", stage: "completed", notes_state: "ready", notes_revision: 2,
    updated_at: "2026-10-15T00:00:04Z" });
  state.noteResults[current.id] = { ...state.notes, title: "自动跟随的新笔记" };
  await page.clock.runFor(2000);
  await expect(page.locator(".overview h3")).toHaveText("自动跟随的新笔记");
  await expect(page.getByRole("button", { name: "最新笔记", exact: true })).toHaveAttribute("aria-pressed", "true");
});

test("阅读旧版本时草稿更新不打断，查看草稿和停止继续仍操作当前任务", async ({ page }) => {
  const state = await mockWorkspace(page);
  previousVersion(state);
  const current = { ...state.tasks["first-task"], id: "first-draft", status: "running", stage: "segments", notes_state: "none", notes_revision: 0 };
  state.tasks[current.id] = state.videos[0].latest_task = current;
  state.noteResults[current.id] = { ...state.notes, title: "本次草稿" };
  await page.clock.install({ time: new Date("2026-10-15T00:00:00Z") });
  await page.clock.pauseAt(new Date("2026-10-15T00:00:01Z"));
  await page.goto("/#summary/first");
  await expect(page.locator(".overview h3")).toHaveText("完整笔记 first");
  await page.getByRole("button", { name: "上一份笔记", exact: true }).click();
  Object.assign(current, { notes_state: "partial", notes_revision: 1 });
  await page.clock.runFor(500);
  await expect(page.getByRole("button", { name: "查看已生成笔记 ↓", exact: true })).toBeVisible();
  await expect(page.locator(".overview h3")).toHaveText("上一份成功笔记");
  await page.getByRole("button", { name: "查看已生成笔记 ↓", exact: true }).click();
  await page.clock.runFor(20);
  await expect(page.locator(".overview h3")).toHaveText("本次草稿");
  await page.getByRole("button", { name: "上一份笔记", exact: true }).click();
  await page.getByRole("button", { name: "停止处理", exact: true }).click();
  await expect(page.locator(".task-panel .status-pill")).toHaveText("已停止");
  await expect(page.locator(".overview h3")).toHaveText("上一份成功笔记");
  await page.getByRole("button", { name: "继续处理", exact: true }).click();
  expect(state.writes).toEqual([`/api/v1/tasks/${current.id}/stop`, `/api/v1/tasks/${current.id}/resume`]);
});

for (const retired of [false, true]) {
  test(`新笔记完成后${retired ? "已淘汰的所选版本切换到最新" : "保留正在阅读的上一份"}`, async ({ page }) => {
    const state = await mockWorkspace(page);
    const previous = previousVersion(state);
    const current = { ...state.tasks["first-task"], id: "first-new", status: "running", stage: "finalizing", notes_state: "none", notes_revision: 0 };
    state.tasks[current.id] = state.videos[0].latest_task = current;
    state.noteResults[current.id] = { ...state.notes, title: "新完成的笔记" };
    await page.clock.install({ time: new Date("2026-10-15T00:00:00Z") });
    await page.clock.pauseAt(new Date("2026-10-15T00:00:01Z"));
    await page.goto("/#summary/first");
    await page.getByRole("button", { name: retired ? "上一份笔记" : "最新笔记", exact: true }).click();
    Object.assign(current, { status: "succeeded", stage: "completed", notes_state: "ready", notes_revision: 1,
      updated_at: "2026-10-15T00:00:03Z" });
    previous.notes_state = "none";
    await page.clock.runFor(500);
    await expect(page.locator(".task-panel .status-pill")).toHaveText("已完成");
    await expect(page.locator(".overview h3")).toHaveText(retired ? "新完成的笔记" : "完整笔记 first");
    await expect(page.getByRole("button", { name: retired ? "最新笔记" : "上一份笔记", exact: true })).toHaveAttribute("aria-pressed", "true");
  });
}

test("同一视频快速切换版本时迟到正文和依据不能覆盖当前版本", async ({ page }) => {
  const state = await mockWorkspace(page);
  const previous = previousVersion(state);
  let release!: () => void;
  const delayed = new Promise<void>(resolve => { release = resolve; });
  let waiting = 0;
  await page.route(`**/api/v1/analyses/${previous.id}`, async route => {
    waiting++;
    await delayed;
    await route.fulfill({ json: state.noteResults[previous.id] });
  });
  await page.route("**/api/v1/videos/first/evidence*", async route => {
    if (new URL(route.request().url()).searchParams.get("task_id") !== previous.id) return route.fallback();
    waiting++;
    await delayed;
    await route.fulfill({ json: state.evidenceByTask[previous.id] });
  });
  await page.goto("/#summary/first");
  await page.getByRole("button", { name: "上一份笔记", exact: true }).click();
  await expect.poll(() => waiting).toBe(2);
  await page.getByRole("button", { name: "最新笔记", exact: true }).click();
  release();
  await expect(page.locator(".overview h3")).toHaveText("完整笔记 first");
  await expect(page.locator(".transcript-actions a").first()).toHaveAttribute("href", /task_id=first-task&format=txt$/);
  await expect(page.locator(".evidence-panel")).not.toContainText("上一份识别配置的原文");
});

test("停止完成后可继续或重新生成笔记，重新生成创建新任务", async ({ page }) => {
  const state = await mockWorkspace(page);
  const paused = { ...state.tasks["first-task"], status: "running", stage: "summarizing", notes_state: "none", notes_revision: 0,
    config: { outline_preview: "旧任务的大纲导览。" } };
  state.videos[0].latest_task = state.tasks[paused.id] = paused;
  const generated = { ...paused, id: "new-task", status: "queued", config: {}, created_at: "2026-10-16T00:00:00Z" };
  state.tasks[generated.id] = generated;
  await page.route("**/api/v1/tasks/first-task/stop", async (route) => {
    state.writes.push(new URL(route.request().url()).pathname);
    paused.status = "stopping";
    await route.fulfill({ json: { task_id: paused.id, status: paused.status } });
  });
  await page.route("**/api/v1/videos/first/analyses", async (route) => {
    if (route.request().method() === "GET") return route.fallback();
    state.writes.push(new URL(route.request().url()).pathname);
    state.videos[0].latest_task = generated;
    await route.fulfill({ status: 202, json: { task_id: generated.id, status: generated.status } });
  });
  await page.clock.install({ time: new Date("2026-10-16T00:00:00Z") });
  await page.clock.pauseAt(new Date("2026-10-16T00:00:01Z"));
  await page.goto("/#summary/first");
  const regenerate = page.getByRole("button", { name: "重新生成笔记", exact: true });
  const resume = page.getByRole("button", { name: "继续处理", exact: true });
  await expect(page.getByRole("button", { name: "停止处理", exact: true })).toBeVisible();
  await expect(regenerate).toHaveCount(0);
  await page.getByRole("button", { name: "停止处理", exact: true }).click();
  await expect(page.getByRole("button", { name: "正在停止…", exact: true })).toBeDisabled();
  await expect(regenerate).toHaveCount(0);
  paused.status = "paused";
  await page.clock.runFor(2000);
  await expect(resume).toBeVisible();
  await expect(regenerate).toBeVisible();
  await expect(page.locator(".video-overview")).toContainText("旧任务的大纲导览。");
  expect((await resume.boundingBox())!.y).toBe((await regenerate.boundingBox())!.y);
  await page.locator(".task-panel").screenshot({ path: test.info().outputPath("paused-actions.png") });
  await regenerate.click();
  const setup = page.getByRole("dialog", { name: "选择处理模型" });
  await setup.getByRole("button", { name: "取消", exact: true }).click();
  await expect(resume).toBeVisible();
  expect(state.writes).toEqual(["/api/v1/tasks/first-task/stop"]);
  await regenerate.click();
  await setup.getByRole("button", { name: "开始处理", exact: true }).click();
  await expect(setup).toHaveCount(0);
  await expect(page.getByRole("button", { name: "停止处理", exact: true })).toBeVisible();
  await expect(resume).toHaveCount(0);
  await expect(regenerate).toHaveCount(0);
  await expect(page.locator(".video-overview")).toHaveCount(0);
  expect(state.tasks[paused.id].status).toBe("paused");
  expect(state.videos[0].latest_task.id).toBe(generated.id);
  expect(state.writes).toEqual(["/api/v1/tasks/first-task/stop", "/api/v1/videos/first/analyses"]);
});

test("设置覆盖详情，关闭和前进后退保留同一播放器、引用与播放时间", async ({
  page,
}) => {
  await mockWorkspace(page);
  await page.goto("/#summary/first");
  await expect(page.locator(".overview h3")).toHaveText("完整笔记 first");
  await page.getByRole("button", { name: "查看本章依据", exact: true }).click();
  await expect
    .poll(() => page.locator("video").evaluate((video) => video.readyState))
    .toBeGreaterThan(0);
  await page.locator("video").evaluate((video) => {
    video.currentTime = 7;
    (window as any).originalPlayer = video;
  });
  await page.getByRole("link", { name: "⚙ 设置", exact: true }).click();
  await page.getByRole("button", { name: "关闭设置", exact: true }).click();
  await expect(page).toHaveURL(/#summary\/first$/);
  await expect(
    page.getByRole("button", { name: "收起本章依据", exact: true }),
  ).toBeVisible();
  expect(
    await page
      .locator("video")
      .evaluate(
        (video) =>
          video === (window as any).originalPlayer && video.currentTime === 7,
      ),
  ).toBe(true);
  await page.goBack();
  await expect(
    page.getByRole("dialog", { name: "设置", exact: true }),
  ).toBeVisible();
  await page.goForward();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  expect(
    await page
      .locator("video")
      .evaluate(
        (video) =>
          video === (window as any).originalPlayer && video.currentTime === 7,
      ),
  ).toBe(true);
});

test("直接打开设置返回首页，空和未知地址兼容原导航", async ({ page }) => {
  await mockWorkspace(page);
  await page.goto("/#settings");
  await page.getByRole("button", { name: "关闭设置", exact: true }).click();
  await expect(page.locator(".home-screen h1")).toHaveText(
    "今天，想从哪里开始？",
  );
  for (const hash of ["#", "#unknown", "#summary/one/extra", ""]) {
    await page.evaluate((hash) => {
      location.hash = hash;
    }, hash);
    await expect(page.locator(".home-screen h1")).toHaveText(
      "今天，想从哪里开始？",
    );
  }
  await page.evaluate(() => {
    location.hash = "#notes";
  });
  await expect(page.locator(".note-search h1")).toHaveText("笔记中心");
});

test("快速切换视频忽略旧笔记及证据响应，离开后停止旧轮询", async ({ page }) => {
  const state = await mockWorkspace(page);
  state.delayFirst = true;
  await page.goto("/#summary/first");
  await expect.poll(() => state.slowReadStarted).toBe(true);
  await page
    .locator(".sidebar .library-item")
    .filter({ hasText: "second.mp4" })
    .click();
  await expect(page.locator(".overview h3")).toHaveText("完整笔记 second");
  state.release();
  await expect(page.locator(".overview h3")).toHaveText("完整笔记 second");
  await expect(page.getByRole("alert")).toHaveCount(0);
  state.tasks["first-task"].status = "running";
  await page.goto("/#summary/first");
  await page.reload();
  await expect
    .poll(
      () =>
        state.reads.filter((path) => path === "/api/v1/tasks/first-task")
          .length,
    )
    .toBeGreaterThan(0);
  await page
    .locator(".sidebar .library-item")
    .filter({ hasText: "second.mp4" })
    .click();
  const count = state.reads.filter(
    (path) => path === "/api/v1/tasks/first-task",
  ).length;
  await page.clock.install({ time: new Date("2026-10-14T00:00:00Z") });
  await page.clock.pauseAt(new Date("2026-10-14T00:00:01Z"));
  await page.clock.runFor(6000);
  expect(
    state.reads.filter((path) => path === "/api/v1/tasks/first-task"),
  ).toHaveLength(count);
});

test("OCR 阶段更新保持两秒轮询，失败五秒重查，停止继续重试恢复同一任务", async ({
  page,
}) => {
  const state = await mockWorkspace(page);
  state.videos[0].latest_task = state.tasks["first-task"] = {
    ...state.tasks["first-task"],
    status: "running",
    notes_state: "none", notes_revision: 0,
  };
  await page.clock.install({ time: new Date("2026-10-14T00:00:00Z") });
  await page.clock.pauseAt(new Date("2026-10-14T00:00:01Z"));
  await page.goto("/#summary/first");
  await expect(page.locator(".player-panel h2")).toHaveText("first.mp4");
  const reads = () =>
    state.reads.filter((path) => path === "/api/v1/tasks/first-task").length;
  await page.clock.runFor(499);
  expect(reads()).toBe(0);
  await page.clock.runFor(1);
  await expect.poll(reads).toBe(1);
  const evidenceReads = state.reads.filter((path) =>
    path.endsWith("/evidence"),
  ).length;
  state.tasks["first-task"].stage = "ocr:2/10";
  await page.clock.runFor(1999);
  expect(reads()).toBe(1);
  await page.clock.runFor(1);
  await expect.poll(reads).toBe(2);
  expect(state.reads.filter((path) => path.endsWith("/evidence"))).toHaveLength(
    evidenceReads,
  );
  state.failPoll = true;
  await page.clock.runFor(2000);
  await expect.poll(reads).toBe(3);
  await expect(page.getByRole("alert")).toContainText("轮询失败");
  await page.clock.runFor(4999);
  expect(reads()).toBe(3);
  await page.clock.runFor(1);
  await expect.poll(reads).toBe(4);
  await page.getByRole("button", { name: "停止处理", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "继续处理", exact: true }),
  ).toBeVisible();
  const pausedReads = reads();
  await page.clock.runFor(6000);
  expect(reads()).toBe(pausedReads);
  await page.getByRole("button", { name: "继续处理", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "停止处理", exact: true }),
  ).toBeVisible();
  const resumedReads = reads();
  state.tasks["first-task"].status = "failed";
  state.tasks["first-task"].error = {
    message: "任务失败",
    error_id: "failure",
  };
  await page.clock.runFor(2000);
  await expect.poll(reads).toBeGreaterThan(resumedReads);
  await page
    .getByRole("button", { name: "复用已有识别，重试任务", exact: true })
    .click();
  await expect(
    page.getByRole("button", { name: "停止处理", exact: true }),
  ).toBeVisible();
  const retryReads = reads();
  await page.clock.runFor(2000);
  await expect.poll(reads).toBeGreaterThan(retryReads);
  expect(
    state.writes.filter((path) => /\/(stop|resume|retry)$/.test(path)),
  ).toEqual([
    "/api/v1/tasks/first-task/stop",
    "/api/v1/tasks/first-task/resume",
    "/api/v1/tasks/first-task/retry",
  ]);
});

test("后台列表刷新不能覆盖正在跟踪的任务，返回列表后继续刷新", async ({
  page,
}) => {
  const state = await mockWorkspace(page);
  for (const video of state.videos)
    video.latest_task = {
      ...video.latest_task,
      status: "running",
      stage: "ocr:1/10",
      notes_state: "none", notes_revision: 0,
    };
  state.tasks["first-task"] = {
    ...state.videos[0].latest_task,
    stage: "ocr:5/10",
  };
  await page.clock.install({ time: new Date("2026-10-14T00:00:00Z") });
  await page.clock.pauseAt(new Date("2026-10-14T00:00:01Z"));
  await page.goto("/#summary/first");
  await expect(page.locator(".task-activity")).toContainText("1/10");
  await page.clock.runFor(500);
  await expect(page.locator(".task-activity")).toContainText("5/10");
  state.videos[0].latest_task.status = "queued";
  await page.clock.runFor(2000);
  await expect(page.locator(".task-activity")).toContainText("5/10");
  await expect(
    page.locator(".sidebar .library-row.selected .file-glyph"),
  ).toHaveClass(/running/);
  await page.locator(".detail-back").click();
  state.videos[0].latest_task.status = "succeeded";
  await page.clock.runFor(2000);
  await expect(page.locator(".file-list tbody tr").first()).toContainText(
    "已完成",
  );
});

test("时间戳只定位视频，保留笔记位置与按钮焦点并复用同一播放器", async ({ page }) => {
  const state = await mockWorkspace(page);
  state.notes.chapters[0].start_seconds = 0.5;
  state.notes.chapters[0].markdown = "笔记继续在当前位置阅读。\n\n".repeat(20);
  state.evidence[0].start_seconds = 0.25;
  state.evidence.find((item) => item.id === "frame-1")!.start_seconds = 0.75;
  await page.goto("/#summary/first");
  const player = page.locator("video");
  await expect.poll(() => player.evaluate((video: HTMLVideoElement) => video.readyState)).toBeGreaterThanOrEqual(2);
  await player.evaluate((video) => { video.dataset.retained = "yes"; });
  async function seek(button: Locator, seconds: number) {
    await button.scrollIntoViewIfNeeded();
    const before = await page.evaluate(() => ({ x: window.scrollX, y: window.scrollY }));
    expect(before.y).toBeGreaterThan(0);
    await button.click();
    await expect(button).toBeFocused();
    await expect.poll(() => player.evaluate((video: HTMLVideoElement) => video.currentTime)).toBeCloseTo(seconds, 2);
    expect(await page.evaluate(() => ({ x: window.scrollX, y: window.scrollY }))).toEqual(before);
    await expect(player).toHaveAttribute("data-retained", "yes");
    expect(await player.evaluate((video: HTMLVideoElement) => video.paused)).toBe(true);
  }
  const chapterTime = page.locator("#note-block-C1 > .time-button");
  await seek(chapterTime, 0.5);
  await page.locator("#note-block-C1 .cite-button").click();
  const citation = page.locator("#block-evidence-C1");
  await seek(citation.locator(".time-button").first(), 0.25);
  await seek(citation.locator(".citation-content").last(), 0.75);
  const transcript = page.locator(".evidence-item").first();
  await transcript.locator("summary").click();
  await seek(transcript.locator(".time-button"), 0.25);
  await player.evaluate((video: HTMLVideoElement) => { video.currentTime = 1; });
  // 重复点击相同时间戳也必须重新定位。
  await seek(transcript.locator(".time-button"), 0.25);
  expect(state.writes).toEqual([]);
});

test("引用展开和回看锚点、转录三十条分页及切换视频重置", async ({ page }) => {
  await mockWorkspace(page);
  await page.goto("/#summary/first");
  await expect(page.locator(".overview h3")).toHaveText("完整笔记 first");
  await expect(page.locator(".evidence-item")).toHaveCount(30);
  await page.getByRole("button", { name: "显示更多转录", exact: true }).click();
  await expect(page.locator(".evidence-item")).toHaveCount(36);
  await page.getByRole("tab", { name: /图像识别/ }).click();
  await expect(page.locator(".evidence-item")).toHaveCount(1);
  await page.getByRole("button", { name: "查看本章依据", exact: true }).click();
  await expect(page.locator("#block-evidence-C1 img")).toBeVisible();
  await expect
    .poll(() => page.locator("video").evaluate((video) => video.readyState))
    .toBeGreaterThan(0);
  await page.locator("#block-evidence-C1 .time-button").first().click();
  await page.getByRole("button", { name: "回到笔记 ↓", exact: true }).click();
  await expect(page.locator("#note-block-C1")).toBeFocused();
  await expect(
    page.getByRole("button", { name: "收起本章依据", exact: true }),
  ).toBeVisible();
  await expect(page.locator("#video-notes").getByRole("link", { name: "下载 TXT", exact: true })).toHaveAttribute(
    "href",
    "/api/v1/analyses/first-task/notes",
  );
  await expect(page.locator(".transcript-actions a").last()).toHaveAttribute(
    "href",
    /task_id=first-task&format=json$/,
  );
  await page
    .locator(".sidebar .library-item")
    .filter({ hasText: "second.mp4" })
    .click();
  await expect(page.locator(".overview h3")).toHaveText("完整笔记 second");
  await expect(page.locator("#block-evidence-C1")).toHaveCount(0);
  await expect(page.locator(".evidence-item")).toHaveCount(30);
  await expect(page.getByRole("tab", { name: /语音转录/ })).toHaveAttribute(
    "aria-selected",
    "true",
  );
  await expect(page.locator(".player-caption")).toContainText(
    "点击时间戳，定位视频",
  );
});

test("健康配置初读与设置保存刷新各自生效，识别和网络保存保留弹窗", async ({
  page,
}) => {
  const state = await mockWorkspace(page);
  await page.goto("/#summary/first");
  await expect(page.locator(".overview h3")).toHaveText("完整笔记 first");
  expect(state.healthReads).toBe(1);
  await page.getByRole("link", { name: "⚙ 设置", exact: true }).click();
  await page.getByRole("tab", { name: "语音识别模型", exact: true }).click();
  await page.getByRole("button", { name: "保存识别设置", exact: true }).click();
  await expect.poll(() => state.healthReads).toBe(2);
  await expect(page.getByRole("dialog")).toBeVisible();
  await page.getByRole("tab", { name: "网络代理", exact: true }).click();
  await page.getByRole("button", { name: "保存代理设置", exact: true }).click();
  await expect(page.locator(".notice[role='status']")).toContainText(
    "已保存，用于新的解析和下载",
  );
  expect(state.healthReads).toBe(2);
  await page.getByRole("tab", { name: "API设置", exact: true }).click();
  await page
    .getByRole("button", { name: "删除已保存密钥", exact: true })
    .click();
  await expect.poll(() => state.healthReads).toBe(3);
  await expect(page.getByRole("dialog")).toBeVisible();
  state.modelConfigured = false;
  await page.getByRole("button", { name: "保存配置", exact: true }).click();
  await expect.poll(() => state.healthReads).toBe(4);
  await expect(page.getByRole("dialog")).toBeVisible();
  await page.getByRole("button", { name: "关闭设置", exact: true }).click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await expect(
    page.locator(".notice").filter({ hasText: "本地识别已就绪" }),
  ).toBeVisible();
});

test("文件类型缺少容器、null 或空字符串均回退 MP4", async ({ page }) => {
  const state = await mockWorkspace(page);
  state.videos[0].media_info = {};
  state.videos[1].media_info = { container: null };
  await page.goto("/#summary");
  await expect(page.locator(".file-list tbody tr td:nth-child(2)")).toHaveText([
    "视频 · MP4",
    "视频 · MP4",
  ]);
  state.videos[0].media_info = { container: "" };
  await page.reload();
  await expect(
    page.locator(".file-list tbody tr td:nth-child(2)").first(),
  ).toHaveText("视频 · MP4");
});


test("整章Markdown按原顺序展示，末尾存疑区默认折叠且没有时间戳", async ({ page }) => {
  const state = await mockWorkspace(page);
  state.notes.chapters[0].markdown = "完整的**解释性正文**。\n\n#### 不同群体\n\n- 第一类群体的特点。\n- 第二类群体的特点。";
  state.notes.source_issues = [{ id: "I1", chapter_id: "C1", kind: "source_issue", quote: "转录 0", reason: "具体名称无法确认。", evidence_ids: ["speech-0"], critical: true }];
  await page.goto("/#summary/first");
  await expect(page.locator(".note-markdown").first()).toContainText("完整的解释性正文。");
  await expect(page.locator(".note-markdown strong")).toHaveText("解释性正文");
  await expect(page.locator(".note-markdown h4")).toHaveText("不同群体");
  await expect(page.locator(".note-markdown > .markdown-content > ul > li")).toHaveCount(2);
  const issues = page.getByRole("complementary", { name: "存疑部分" });
  const toggle = issues.locator("summary");
  await expect(toggle).toHaveText("存疑部分：");
  await expect(issues.locator("ul")).toBeHidden();
  await issues.screenshot({ path: test.info().outputPath("source-issues-collapsed.png") });
  await toggle.click();
  await expect(issues.locator("ul")).toBeVisible();
  await expect(issues).toContainText("原文：转录 0");
  await expect(issues).toContainText("具体名称无法确认。");
  await expect(toggle).toHaveCSS("font-size", "15px");
  await expect(issues.locator("p").first()).toHaveCSS("font-size", "14px");
  await expect(issues.locator("button,time,.time-button")).toHaveCount(0);
  expect(await issues.innerText()).not.toMatch(/\d{2}:\d{2}/);
  expect(await issues.evaluate(element => element === element.parentElement?.lastElementChild)).toBe(true);
  await issues.screenshot({ path: test.info().outputPath("source-issues-expanded.png") });
  await page.screenshot({ path: test.info().outputPath("block-notes.png"), fullPage: true });
  await toggle.click();
  await expect(issues.locator("ul")).toBeHidden();
  await toggle.press("Enter");
  await expect(issues.locator("ul")).toBeVisible();
  await toggle.press("Space");
  await expect(issues.locator("ul")).toBeHidden();
  expect(state.writes).toEqual([]);
});


test("已清空笔记的历史任务显示空态，不请求已清除正文或提示格式升级", async ({ page }) => {
  const state = await mockWorkspace(page);
  const cleared = { ...state.tasks["first-task"], notes_state: "none" };
  state.tasks["first-task"] = cleared;
  state.videos[0].latest_task = cleared;
  await page.goto("/#summary/first");
  await expect(page.getByText("总结会出现在这里", { exact: true })).toBeVisible();
  await expect(page.locator(".overview,.timeline,.notes-download")).toHaveCount(0);
  await expect(page.getByText("笔记格式已更新，请重新生成。", { exact: true })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "重新生成笔记", exact: true })).toBeVisible();
  await page.reload();
  await expect(page.getByText("总结会出现在这里", { exact: true })).toBeVisible();
  expect(state.writes).toEqual([]);
  expect(state.reads.filter(path => path === "/api/v1/analyses/first-task")).toEqual([]);
  expect(state.writes).toEqual([]);
});



test("整章自由Markdown仅有一个依据入口，汇总本章来源并可返回章节", async ({ page }) => {
  const state = await mockWorkspace(page);
  state.notes.chapters[0].markdown = "总体判断。\n\n#### 出游总量与花费\n\n人数增加，花费增长更慢。\n\n- 画面补充。\n\n#### 交通数据\n\n不同方式呈现不同变化。";
  state.notes.chapters[0].evidence_ids = ["speech-0", "speech-1", "speech-2", "frame-1"];
  const saved = JSON.stringify(state.notes);
  await page.goto("/#summary/first");
  await expect(page.locator(".note-markdown h4")).toHaveCount(2);
  await expect(page.locator(".cite-button")).toHaveCount(1);
  await page.getByRole("button", { name: "查看本章依据", exact: true }).click();
  const citation = page.locator("#block-evidence-C1");
  await expect(citation.locator("tbody tr")).toHaveCount(4);
  await expect(citation.locator("img")).toHaveCount(1);
  await expect.poll(() => page.locator("video").evaluate((video: HTMLVideoElement) => video.readyState)).toBeGreaterThan(0);
  await citation.getByRole("button", { name: "00:01 ↗", exact: true }).click();
  await page.getByRole("button", { name: "回到笔记 ↓", exact: true }).click();
  await expect(page.locator("#note-block-C1")).toBeFocused();
  await page.getByRole("button", { name: "收起本章依据", exact: true }).click();
  await expect(citation).toHaveCount(0);
  expect(JSON.stringify(state.notes)).toBe(saved);
  expect(state.writes).toEqual([]);
});

test("Markdown保留六级标题的原始层级，明暗窄屏下字号递减", async ({ page }) => {
  const state = await mockWorkspace(page);
  state.notes.chapters[0].markdown = Array.from({ length: 6 }, (_, index) =>
    `${"#".repeat(index + 1)} 第${index + 1}级标题\n\n这一段保留完整的解释。`).join("\n\n");
  await page.goto("/#summary/first");
  const body = page.locator(".note-markdown");
  for (let level = 1; level <= 6; level++) {
    await expect(body.getByRole("heading", { level, name: `第${level}级标题` })).toBeVisible();
  }
  await expect(body.locator("p")).toHaveCount(6);
  await expect(page.locator("#note-block-C1 > .note-heading > h3")).toHaveText("第一节");
  for (const width of [1280, 375]) {
    await page.setViewportSize({ width, height: 871 });
    for (const theme of ["light", "dark"]) {
      await page.evaluate(value => document.documentElement.dataset.theme = value, theme);
      const sizes = await body.locator("h1,h2,h3,h4,h5,h6").evaluateAll(headings =>
        headings.map(heading => Number.parseFloat(getComputedStyle(heading).fontSize)));
      expect(sizes.every((size, index) => index === 0 || sizes[index - 1] > size)).toBe(true);
      const chapterSize = await page.locator("#note-block-C1 > .note-heading > h3").evaluate(element => Number.parseFloat(getComputedStyle(element).fontSize));
      expect(chapterSize).toBe(26);
      expect(sizes[0]).toBe(26);
      expect(sizes[2]).toBeCloseTo(18 + 2, 3);
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
      const chapter = page.locator("#note-block-C1");
      await chapter.evaluate(element => element.scrollIntoView({ block: "center" }));
      await chapter.screenshot({ path: test.info().outputPath(`headings-${width}-${theme}.png`) });
    }
  }
  expect(state.writes).toEqual([]);
});

test("Markdown清单保留只读勾选状态，过滤原始HTML输入", async ({ page }) => {
  const state = await mockWorkspace(page);
  state.notes.chapters[0].markdown = '- [x] 已核对原文\n- [ ] 尚待复习\n\n<script>window.__markdownExecuted = true</script>\n<input type="password" value="unsafe">';
  await page.goto("/#summary/first");
  const body = page.locator(".note-markdown");
  const done = body.getByRole("checkbox", { name: "已完成", exact: true });
  const pending = body.getByRole("checkbox", { name: "未完成", exact: true });
  await expect(done).toBeChecked();
  await expect(pending).not.toBeChecked();
  await expect(done).toBeDisabled();
  await expect(pending).toBeDisabled();
  await expect(body.locator("input")).toHaveCount(2);
  await expect(body.locator("script")).toHaveCount(0);
  for (const theme of ["light", "dark"]) {
    await page.evaluate(value => document.documentElement.dataset.theme = value, theme);
    await expect(done).toBeVisible();
    await expect(pending).toBeVisible();
    await body.screenshot({ path: test.info().outputPath(`checklist-${theme}.png`) });
  }
  expect(await page.evaluate(() => (window as any).__markdownExecuted)).toBeUndefined();
  expect(state.writes).toEqual([]);
});

test("Markdown内部链接和跨章节同号脚注定位正确，不改路由或视频时间", async ({ page, context }) => {
  const state = await mockWorkspace(page);
  const markdown = ["[查看说明](#说明) · [第二个说明](#说明-1) · [不存在](#missing)", "",
    "讲解[^注]，再次引用[^注]。", "", "## 说明", "", "这一段解释条件。", "", "## 说明", "",
    "另一段补充。", "", "[^注]: 本章的原文依据。", "", "[外部资料](https://example.com)"].join("\n");
  state.notes.chapters = [state.notes.chapters[0], { ...state.notes.chapters[0], chapter_id: "C2", title: "第二节" }]
    .map(chapter => ({ ...chapter, markdown }));
  await page.goto("/#summary/first");
  await page.emulateMedia({ reducedMotion: "reduce" });
  await expect.poll(() => page.locator("video").evaluate((video: HTMLVideoElement) => video.readyState)).toBeGreaterThan(0);
  await page.locator("video").evaluate((video: HTMLVideoElement) => { video.pause(); video.currentTime = 7; });
  const url = page.url(), pages = context.pages().length;
  const ids = await page.locator(".note-markdown [id]").evaluateAll(elements => elements.map(element => element.id));
  expect(new Set(ids).size).toBe(ids.length);
  for (const chapter of ["C1", "C2"]) {
    const body = page.locator(`#note-block-${chapter} .note-markdown`);
    const headings = body.locator("h2").filter({ hasText: /^说明$/ });
    await body.getByRole("link", { name: "查看说明", exact: true }).click();
    await expect(headings.first()).toBeFocused();
    await body.getByRole("link", { name: "第二个说明", exact: true }).focus();
    await page.keyboard.press("Enter");
    await expect(headings.nth(1)).toBeFocused();
    const refs = body.locator("a[data-footnote-ref]");
    await expect(refs).toHaveCount(2);
    const labelId = await refs.first().getAttribute("aria-describedby");
    expect(await body.locator("[id]").evaluateAll((elements, id) => elements.some(element => element.id === id), labelId)).toBe(true);
    await refs.nth(1).click();
    await expect(body.locator("section[data-footnotes] li")).toBeFocused();
    await body.locator("a[data-footnote-backref]").nth(1).click();
    await expect(refs.nth(1)).toBeFocused();
    await body.getByRole("link", { name: "不存在", exact: true }).click();
    await expect(body.getByRole("link", { name: "外部资料", exact: true })).toHaveAttribute("target", "_blank");
  }
  expect(page.url()).toBe(url);
  expect(context.pages()).toHaveLength(pages);
  expect(await page.locator("video").evaluate((video: HTMLVideoElement) => video.currentTime)).toBeCloseTo(7);
  expect(state.writes).toEqual([]);
});

test("Markdown支持表格引用嵌套列表和代码，窄屏明暗主题可读并提供双下载", async ({ page }) => {
  const state = await mockWorkspace(page);
  const markdown = [
    "#### 结果与解释", "", "**核心判断**：先观察，再比较，保留 `a_b`。", "",
    "1. 查看材料", "   - 保留条件", "   - 对照原文", "2. 给出结论", "",
    "> 只有材料明确提到的内容才进入笔记。", "",
    "| 对象 | 条件 | 现象 | 原因 | 影响 | 例外 | 边界 | 结论 |",
    "| --- | --- | --- | --- | --- | --- | --- | --- |",
    "| 第一组 | 稳定 | 平稳 | 条件充分 | 较小 | 有 | 可验证 | 按条件判断 |", "",
    "```python", 'marker = "**literal**"', "if marker:", "    print(marker)", "```", "",
    "[资料链接](https://example.com)", "",
    '<script>window.__markdownExecuted = true</script>', "",
    "![不加载远程图片](https://example.com/unwanted.png)",
  ].join("\n");
  state.notes.chapters[0] = { ...state.notes.chapters[0], markdown };
  await page.goto("/#summary/first");
  const body = page.locator(".note-markdown");
  await expect(body.locator("h4")).toHaveText("结果与解释");
  await expect(body.locator("strong")).toHaveText("核心判断");
  await expect(body.locator("ol > li")).toHaveCount(2);
  await expect(body.locator("ol ul > li")).toHaveCount(2);
  await expect(body.locator("blockquote")).toContainText("只有材料明确提到");
  await expect(body.locator("table tbody td")).toHaveCount(8);
  await expect(body.locator("pre code")).toHaveText('marker = "**literal**"\nif marker:\n    print(marker)\n');
  await expect(body.getByRole("link", { name: "资料链接" })).toHaveAttribute("rel", "noopener noreferrer");
  await expect(body.locator("script,img")).toHaveCount(0);
  expect(await page.evaluate(() => (window as any).__markdownExecuted)).toBeUndefined();
  await expect(page.locator("#video-notes").getByRole("link", { name: "下载 TXT", exact: true })).toHaveAttribute("href", "/api/v1/analyses/first-task/notes");
  await expect(page.getByRole("link", { name: "下载 Markdown", exact: true })).toHaveAttribute("href", "/api/v1/analyses/first-task/notes?format=md");
  await expect(page.locator(".cite-button")).toHaveCount(1);
  for (const width of [725, 390]) {
    await page.setViewportSize({ width, height: 871 });
    if (width === 725) await page.locator(".sidebar .desktop-sidebar-toggle").click();
    for (const theme of ["light", "dark"]) {
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
      await expect(body).toHaveCSS("font-size", "16px");
      await expect(body.locator("pre")).toHaveCSS("overflow-x", "auto");
      await expect(body.locator(".markdown-table")).toHaveCSS("overflow-x", "auto");
      await page.locator("#note-block-C1").evaluate(element => element.scrollIntoView({ block: "start" }));
      await body.screenshot({ path: test.info().outputPath(`markdown-${width}-${theme}.png`) });
      await page.locator(".theme-toggle").click();
    }
  }
  expect(state.writes).toEqual([]);
});


for (const theme of ["light", "dark"]) test(`代码块保留缩进、复制原文和窄屏滚动-${theme}`, async ({ page }) => {
  const state = await mockWorkspace(page);
  const source = '# 示例\nwhile True:\n    print("文字", 2)\n';
  state.notes.chapters[0].markdown = "行内代码 `sum`。\n\n```python\n" + source + "```\n\n```unknown-language\n<literal>&value\n```\n\n```\n" + "long_value = '" + "x".repeat(200) + "'\n```";
  await page.addInitScript(value => {
    localStorage.setItem("studybuddy-theme", value);
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: {
      writeText: async (text: string) => {
        if ((window as any).__copyFails) throw new Error("clipboard denied");
        (window as any).__copiedCode = text;
      },
    } });
  }, theme);
  await page.goto("/#summary/first");
  const body = page.locator(".note-markdown"), block = body.locator(".note-code-block").first();
  await expect(body.locator(".note-code-block")).toHaveCount(3);
  await expect(block).toHaveCSS("background-color", "rgb(32, 32, 36)");
  await expect(block).toHaveCSS("color", "rgb(237, 237, 240)");
  await expect(block).toHaveCSS("color-scheme", "dark");
  await expect(block.locator(".note-code-language")).toHaveText("python");
  await expect(body.locator(".note-code-language").last()).toHaveText("代码");
  expect(await block.locator("pre code").textContent()).toBe(source);
  await expect(block.locator(".hljs-keyword")).toHaveText("while");
  await expect(block.locator(".hljs-string")).toHaveText('"文字"');
  await expect(block.locator(".hljs-number")).toHaveText("2");
  await expect(body.locator(".note-code-block").nth(1).locator("pre code")).toHaveText("<literal>&value\n");
  await expect(body.locator(".note-code-block").nth(1).locator("pre span")).toHaveCount(0);
  await expect(body.locator(".note-code-block").last().locator("pre span")).toHaveCount(0);
  await expect(body.locator("p code")).toHaveText("sum");
  await block.getByRole("button", { name: "复制代码" }).focus();
  await page.keyboard.press("Enter");
  await expect(block.getByRole("status")).toHaveText("已复制");
  expect(await page.evaluate(() => (window as any).__copiedCode)).toBe(source);
  await page.evaluate(() => { (window as any).__copyFails = true; });
  await block.getByRole("button", { name: "复制代码" }).click();
  await expect(block.getByRole("status")).toHaveText("复制失败，请重试");
  for (const width of [1280, 375]) {
    await page.setViewportSize({ width, height: 871 });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    const long = body.locator(".note-code-block pre").last();
    await expect(long).toHaveCSS("white-space", "pre");
    await expect(long).toHaveCSS("font-size", "16px");
    expect(await long.evaluate(element => element.scrollWidth > element.clientWidth)).toBe(true);
  }
  expect(state.writes).toEqual([]);
});

test("关键疑问仍在折叠存疑区，省略无效引文且不增加需核查标记", async ({ page }) => {
  const state = await mockWorkspace(page);
  state.notes.source_issues = [{ id: "I1", chapter_id: "C1", kind: "source_issue", quote: null,
    reason: "关键结论的适用条件无法确认。", evidence_ids: ["speech-0"], critical: true }];
  await page.goto("/#summary/first");
  await expect(page.getByText("需核查", { exact: true })).toHaveCount(0);
  const issues = page.getByRole("complementary", { name: "存疑部分" });
  await expect(issues.locator("ul")).toBeHidden();
  await issues.locator("summary").click();
  await expect(issues).toContainText("关键结论的适用条件无法确认。");
  await expect(issues).not.toContainText("原文：");
  await expect(page.locator(".note-markdown")).toContainText("要点");
});

test("v3正文停止使用且不提示升级，不自动重新生成", async ({ page }) => {
  const state = await mockWorkspace(page);
  state.tasks["first-task"].notes_state = "none";
  state.tasks["first-task"].notes_revision = 0;
  state.videos[0].latest_task = state.tasks["first-task"];
  await page.goto("/#summary/first");
  await expect(page.getByText("总结会出现在这里", { exact: true })).toBeVisible();
  await expect(page.locator(".overview,.timeline,.notes-download")).toHaveCount(0);
  await expect(page.getByText("笔记格式已更新，请重新生成。", { exact: true })).toHaveCount(0);
  expect(state.reads.filter(path => path === "/api/v1/analyses/first-task")).toEqual([]);
  expect(state.writes).toEqual([]);
});


for (const segmented of [false, true]) {
  test(`服务端步骤与标签契约：${segmented ? "分段" : "普通"}`, async ({ page }) => {
    const state = await mockWorkspace(page);
    Object.assign(state.tasks["first-task"], { status: "paused", stage: "ocr:3/12", notes_state: "none", notes_revision: 0,
      timings: { planning: 1, summarizing: 2, total: 3 }, progress: { segmented, percent: 40,
        completed_steps: 2, current_step: 3, processed: 3, total: 12 } });
    await page.goto("/#summary/first");
    const steps = page.locator(".workflow-steps [data-stage]");
    await expect(steps).toHaveCount(segmented ? 5 : 6);
    expect(await steps.evaluateAll(items => items.map(item => item.getAttribute("data-stage")))).toEqual(
      segmented ? ["checking", "transcribing", "ocr", "segments", "finalizing"]
        : ["checking", "transcribing", "ocr", "planning", "summarizing", "finalizing"]);
    await expect(page.locator('[data-current="true"]')).toHaveAttribute("data-stage", "ocr");
    await expect(page.locator('[data-done="true"]')).toHaveCount(2);
    await expect(page.locator(".task-activity")).toContainText("提取画面文字 · 3/12");
    await expect(page.locator(".timings")).toContainText("规划视频章节 1.0s");
    await expect(page.locator(".timings")).toContainText("生成章节正文 2.0s");
    await expect(page.locator(".timings")).toContainText("本次总耗时 3.0s");
    expect(state.reads.filter(path => path === "/api/v1/analyses/first-task")).toEqual([]);
  });
}

test("无任务显示待开始，完成任务同版本正文只读取一次", async ({ page }) => {
  const state = await mockWorkspace(page);
  state.videos[0].latest_task = null as any;
  await page.goto("/#summary/first");
  await expect(page.locator(".task-panel")).toContainText("等待开始处理");
  await expect(page.locator(".workflow-steps")).toHaveCount(0);
  await page.goto("/#summary/second");
  await expect(page.locator(".task-activity")).toContainText("分析完成");
  await expect(page.locator(".note-markdown")).toBeVisible();
  await page.waitForTimeout(2300);
  expect(state.reads.filter(path => path === "/api/v1/analyses/second-task")).toHaveLength(1);
});

for (const theme of ["light", "dark"]) {
  test(`公式支持行内、独立、矩阵及化学式，代码和金额保持原样（${theme}）`, async ({ page }) => {
    const state = await mockWorkspace(page);
    await page.addInitScript(value => localStorage.setItem("studybuddy-theme", value), theme);
    state.notes.chapters[0].markdown = String.raw`质量与能量满足 $E=mc^2$。

$$
x=\frac{-b\pm\sqrt{b^2-4ac}}{2a}
$$

$$
\begin{pmatrix}1 & 2 \\ 3 & 4\end{pmatrix}
$$

$$
\ce{Ag+ + SCN- -> AgSCN v}
$$

| 变量 | 表达式 |
| --- | --- |
| 第一个分量 | $x_1$ |

普通金额为 \$20，代码示例不渲染公式：` + "`$x_i$`。\n\n```latex\n$$x^2$$\n```";
    const saved = JSON.stringify(state.notes);
    await page.goto("/#summary/first");
    const body = page.locator(".note-markdown");
    await expect(body.locator(".katex")).toHaveCount(5);
    await expect(body.locator(".katex-display")).toHaveCount(3);
    const centers = await body.locator(".katex-display").evaluateAll(elements => elements.map(element => {
      const box = element.getBoundingClientRect();
      const formula = element.querySelector(".katex-html")!.getBoundingClientRect();
      return Math.abs((box.left + box.right - formula.left - formula.right) / 2);
    }));
    expect(centers.every(offset => offset < 2)).toBe(true);
    await expect(body.locator(".katex-error")).toHaveCount(0);
    await expect(body.locator("math mfrac")).toHaveCount(1);
    await expect(body.locator("math msqrt")).toHaveCount(1);
    await expect(body.locator("math mtable")).toHaveCount(1);
    await expect(body.locator("annotation").first()).toHaveText("E=mc^2");
    await expect(body.locator("annotation").nth(3)).toHaveText(String.raw`\ce{Ag+ + SCN- -> AgSCN v}`);
    await expect(body.locator("pre code")).toHaveText("$$x^2$$\n");
    await expect(body).toContainText("普通金额为 $20");
    await page.evaluate(() => document.fonts.ready);
    expect(await page.evaluate(() => document.fonts.check("16px KaTeX_Main"))).toBe(true);
    await expect(body.locator(".katex").first()).toHaveCSS("color", await body.evaluate(el => getComputedStyle(el).color));
    await body.screenshot({ path: test.info().outputPath(`math-${theme}.png`) });
    expect(JSON.stringify(state.notes)).toBe(saved);
    expect(state.writes).toEqual([]);
  });
}

test("公式语法错误保留原文且不影响后文，公式不加载外部图片或执行链接", async ({ page }) => {
  const state = await mockWorkspace(page);
  const external: string[] = [];
  page.on("request", request => {
    if (new URL(request.url()).hostname === "math.invalid") external.push(request.url());
  });
  state.notes.chapters[0].markdown = String.raw`无效公式 $\frac{a}{$。

后续公式 $a^2+b^2=c^2$ 仍正常。

$\href{javascript:alert(1)}{click}$

$\includegraphics{https://math.invalid/image.png}$`;
  await page.goto("/#summary/first");
  const body = page.locator(".note-markdown");
  await expect(body.locator(".katex-error")).toHaveText(String.raw`\frac{a}{`);
  await expect(body.locator("annotation").first()).toHaveText("a^2+b^2=c^2");
  await expect(body.locator("img,a,script,iframe")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "查看本章依据", exact: true })).toBeVisible();
  expect(external).toEqual([]);
});

test("窄屏长公式在自身区域滚动，页面和后续正文保持正常宽度", async ({ page }) => {
  const state = await mockWorkspace(page);
  await page.setViewportSize({ width: 390, height: 844 });
  state.notes.chapters[0].markdown = "$$\n" + Array.from({ length: 30 }, (_, i) => `\\frac{x_{${i}}^2}{1+x_{${i}}}`).join("+") + "\n$$\n\n公式后仍可正常阅读。";
  await page.goto("/#summary/first");
  const formula = page.locator(".note-markdown .katex-display");
  await expect(formula).toHaveCount(1);
  await expect(formula).toHaveCSS("overflow-x", "auto");
  expect(await formula.evaluate(el => el.scrollWidth > el.clientWidth)).toBe(true);
  await formula.evaluate(el => { el.scrollLeft = 50; });
  expect(await formula.evaluate(el => el.scrollLeft)).toBeGreaterThan(0);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await expect(page.locator(".note-markdown")).toContainText("公式后仍可正常阅读。");
  await page.locator(".note-markdown").screenshot({ path: test.info().outputPath("math-mobile.png") });
});


test("正文与依据独立失败可分别重读，不创建生成任务", async ({ page }) => {
  const state = await mockWorkspace(page);
  let failNotes = true, failSources = true;
  await page.route("**/api/v1/analyses/first-task", async route => failNotes
    ? route.fulfill({ status: 503, json: { error: { message: "正文暂不可用", error_id: "note-read", log_saved: true } } }) : route.fallback());
  await page.route("**/api/v1/videos/first/evidence*", async route => failSources
    ? route.fulfill({ status: 503, json: { error: { message: "依据暂不可用", error_id: "source-read", log_saved: true } } }) : route.fallback());
  await page.goto("/#summary/first");
  const notes = page.locator("#video-notes");
  await expect(notes.getByRole("button", { name: "重新读取笔记", exact: true })).toBeVisible();
  failNotes = false;
  await notes.getByRole("button", { name: "重新读取笔记", exact: true }).click();
  await expect(notes.locator(".overview h3")).toHaveText("完整笔记 first");
  await notes.getByRole("button", { name: "查看本章依据" }).click();
  await expect(notes.getByRole("button", { name: "重新读取依据" })).toBeVisible();
  failSources = false;
  await notes.getByRole("button", { name: "重新读取依据" }).click();
  await expect(notes.locator(".citation-table")).toContainText("转录 0");
  expect(state.writes).toEqual([]);
});

test("刷新正文失败保留外框和章节节点，重读成功不折叠依据", async ({ page }) => {
  const state = await mockWorkspace(page);
  const task = state.tasks["first-task"];
  Object.assign(task, { status: "running", notes_state: "partial", notes_revision: 1 });
  state.notes.chapters[0].markdown = "已有正文，保留阅读位置。\n\n".repeat(30);
  await page.clock.install({ time: new Date("2026-10-15T00:00:00Z") });
  await page.clock.pauseAt(new Date("2026-10-15T00:00:01Z"));
  await page.goto("/#summary/first");
  const notes = page.locator("#video-notes"), chapter = page.locator("#note-block-C1");
  await chapter.locator(".cite-button").click();
  await notes.evaluate(el => { el.dataset.preserved = "yes"; });
  await chapter.evaluate(el => { el.dataset.preserved = "yes"; el.scrollIntoView(); });
  const position = await chapter.evaluate(el => el.getBoundingClientRect().top);
  let failed = true;
  await page.route("**/api/v1/analyses/first-task", route => failed
    ? route.fulfill({ status: 503, json: { error: { message: "刷新失败", error_id: "refresh", log_saved: true } } }) : route.fallback());
  task.notes_revision = 2;
  await page.clock.runFor(500);
  await expect(notes.getByRole("button", { name: "重新读取笔记" })).toHaveCount(1);
  await expect(chapter).toContainText("已有正文");
  await expect(notes).toHaveAttribute("data-preserved", "yes");
  await expect(chapter).toHaveAttribute("data-preserved", "yes");
  // 状态行改变文档高度时，检查读者正在看的内容在视口中的位置。
  expect(Math.abs(await chapter.evaluate(el => el.getBoundingClientRect().top) - position)).toBeLessThan(3);
  failed = false;
  await notes.getByRole("button", { name: "重新读取笔记" }).click();
  await expect(notes.getByRole("button", { name: "重新读取笔记" })).toHaveCount(0);
  await expect(chapter).toHaveAttribute("data-preserved", "yes");
  await expect(chapter.locator(".cite-button")).toHaveAttribute("aria-expanded", "true");
});

test("版本列表慢刷新或失败不淘汰所选版本，独立重读确认后才回退", async ({ page }) => {
  const state = await mockWorkspace(page);
  previousVersion(state);
  const current = { ...state.tasks["first-task"], id: "first-new", status: "running", notes_state: "none", notes_revision: 0 };
  state.tasks[current.id] = state.videos[0].latest_task = current;
  state.noteResults[current.id] = { ...state.notes, title: "新笔记" };
  await page.clock.install({ time: new Date("2026-10-15T00:00:00Z") });
  await page.clock.pauseAt(new Date("2026-10-15T00:00:01Z"));
  await page.goto("/#summary/first");
  const notes = page.locator("#video-notes");
  await notes.getByRole("button", { name: "上一份笔记" }).click();
  await expect(notes.locator(".overview h3")).toHaveText("上一份成功笔记");
  let release!: () => void;
  const held = new Promise<void>(resolve => { release = resolve; });
  let mode: "held" | "failed" | "ready" = "held";
  await page.route("**/api/v1/videos/first/analyses", async route => {
    if (mode === "held") await held;
    if (mode !== "ready") return route.fulfill({ status: 503, json: { error: { message: "列表刷新失败", error_id: "versions", log_saved: true } } });
    return route.fallback();
  });
  Object.assign(current, { status: "succeeded", notes_state: "ready", notes_revision: 1, updated_at: "2026-10-16T00:00:00Z" });
  await page.clock.runFor(500);
  await expect(notes.getByText("正在读取版本列表…")).toHaveCount(1);
  await expect(notes.locator(".overview h3")).toHaveText("上一份成功笔记");
  mode = "failed"; release();
  await expect(notes.getByRole("button", { name: "重新读取版本列表" })).toHaveCount(1);
  await expect(notes.locator(".overview h3")).toHaveText("上一份成功笔记");
  mode = "ready";
  await notes.getByRole("button", { name: "重新读取版本列表" }).click();
  await expect(notes.locator(".overview h3")).toHaveText("新笔记");
  expect(state.writes).toEqual([]);
});
