import { test, expect, type Page } from "@playwright/test";
import { taskResponse } from "./support/taskContract";
import { settingsRead } from "./support/settings";

async function workspace(page: Page, kind: "video" | "document") {
  const time = "2026-10-27T00:00:00Z";
  const task = { id: "tracked", video_id: "file", document_id: "file", status: "paused", stage: "checking", config: {}, timings: {}, warnings: [], error: null,
    created_at: time, updated_at: time, notes_state: "none", notes_revision: 0, request_count: 0, request_limit: 80,
    progress: { stage_labels: {}, segment: 0, total_segments: 1 } };
  const state = { task, latest: true, failures: 0, reads: 0, writes: [] as string[], bodies: [] as any[], failWrite: false, missing: false, failList: false,
    extraction: null as any, listDelay: 0, active: 0, maxActive: 0, lists: 0, fileId: "file" };
  const value = () => kind === "video" ? taskResponse(state.task) : state.task;
  const file = () => ({ id: state.fileId, kind, filename: "恢复测试", duration: 120, size_bytes: 10, created_at: time,
    format: "txt", unit_count: 1, character_count: 10, extracted: true, warnings: [], latest_task: state.latest ? value() : null, extraction: state.extraction });
  await page.route("**/api/v1/**", async route => {
    const path = new URL(route.request().url()).pathname.replace("/api/v1", "");
    if (await settingsRead(route)) return;
    if (route.request().method() === "POST" && /\/(stop|resume|retry)$|\/analyses$/.test(path)) {
      state.writes.push(path);
      state.bodies.push(route.request().postDataJSON());
      if (state.failWrite) return route.fulfill({ status: 400, json: { error: { message: "提交失败，状态未变", error_id: "write" } } });
      state.task.status = path.endsWith("stop") ? "paused" : "running";
      return route.fulfill({ json: { task_id: state.task.id, status: state.task.status } });
    }
    if (path === "/videos" || path === "/documents") {
      if (state.failList) return route.fulfill({ status: 503, json: { error: { message: "列表暂不可用", error_id: "list" } } });
      if (path === (kind === "video" ? "/videos" : "/documents")) {
        state.lists++; state.active++; state.maxActive = Math.max(state.maxActive, state.active);
        if (state.listDelay) await new Promise(resolve => setTimeout(resolve, state.listDelay));
        state.active--;
        return route.fulfill({ json: [file()] });
      }
      return route.fulfill({ json: [] });
    }
    if (/^\/(tasks|document-tasks)\/tracked$/.test(path)) {
      state.reads++;
      if (state.missing) return route.fulfill({ status: 404, json: { error: { message: "任务不存在", error_id: "missing" } } });
      if (state.failures-- > 0) return route.fulfill({ status: 503, json: { error: { message: "暂时断开", error_id: "read" } } });
      return route.fulfill({ json: value() });
    }
    if (path.endsWith("/extraction")) return route.fulfill({ json: state.extraction });
    if (path.endsWith("/analysis-preview")) return route.fulfill({ json: { eligible: true, character_count: 10, estimated_segments: 1,
      reason: null, limits: { characters: 100000, segments: 10, requests: 80 } } });
    if (path.endsWith("/analyses") || path.endsWith("/evidence")) return route.fulfill({ json: [] });
    if (path.endsWith("/content")) return route.fulfill({ json: { units: [], total: 0, offset: 0 } });
    if (path.endsWith("/playback")) return route.fulfill({ json: { status: "unprepared", direct: true, original_url: "/empty.mp4", mime_type: "video/mp4", error: null } });
    if (path === "/health") return route.fulfill({ json: { model_configured: true, supported_video_extensions: ["mp4"], max_upload_bytes: 1000000 } });
    if (path === "/diagnostics/client-errors") return route.fulfill({ json: { error_id: "test", log_saved: true } });
    throw new Error(`Unexpected ${route.request().method()} ${path}`);
  });
  return state;
}

for (const kind of ["video", "document"] as const) {
  test(`${kind} 回执已接受后读取失败仍同步且不重复提交`, async ({ page }) => {
    const state = await workspace(page, kind);
    await page.clock.install();
    await page.goto("/#summary/file");
    state.failures = 2;
    await page.getByRole("button", { name: "继续处理", exact: true }).click();
    await expect(page.getByText("操作已接受，正在同步状态", { exact: true })).toBeVisible();
    await expect(page.getByRole("button", { name: "继续处理", exact: true })).toHaveCount(0);
    await page.clock.runFor(5100);
    await expect.poll(() => state.reads).toBe(2);
    await page.clock.runFor(5100);
    await expect(page.getByText("操作已接受，正在同步状态", { exact: true })).toHaveCount(0);
    await expect(page.getByRole("button", { name: "停止处理", exact: true })).toBeEnabled();
    expect(state.writes).toHaveLength(1);
  });
}

test("提取完成使用回执接管任务，列表尚无 latest_task", async ({ page }) => {
  const state = await workspace(page, "document");
  state.latest = false;
  state.task.status = "running";
  state.extraction = { source_id: "file", document_id: "file", task_id: "tracked", status: "running", parser: "local", progress: {}, error: null };
  await page.clock.install();
  await page.goto("/#summary/file");
  await expect(page.getByRole("heading", { name: "恢复测试", exact: true })).toBeVisible();
  state.extraction.status = "done";
  await page.clock.runFor(2100);
  await expect(page.getByRole("button", { name: "停止处理", exact: true })).toBeEnabled();
  expect(state.reads).toBeGreaterThan(0);
  expect(state.writes).toEqual([]);
});

test("首次列表失败结束加载，详情也能手动重新读取", async ({ page }) => {
  const state = await workspace(page, "document");
  state.failList = true;
  await page.goto("/#summary/file");
  await expect(page.getByText("文件列表读取失败，请重新读取。", { exact: true })).toBeVisible();
  await expect(page.getByText("文件不存在或已被删除。", { exact: true })).toHaveCount(0);
  state.failList = false;
  await page.getByRole("button", { name: "重新读取", exact: true }).click();
  await expect(page.getByRole("heading", { name: "恢复测试", exact: true })).toBeVisible();
});

test("慢后台刷新串行读取并能持续更新", async ({ page }) => {
  const state = await workspace(page, "video");
  state.task.status = "running";
  await page.goto("/#summary");
  await expect(page.locator(".file-list .file-name")).toHaveText(["恢复测试"]);
  state.listDelay = 2600;
  await expect.poll(() => state.lists, { timeout: 15000 }).toBeGreaterThanOrEqual(3);
  expect(state.maxActive).toBe(1);
  state.task.status = "paused";
  await expect(page.locator(".file-list .status-pill")).toHaveText(["已停止"]);
  state.listDelay = 0;
  await page.goto("/#home");
  await page.unrouteAll({ behavior: "wait" });
});

for (const kind of ["video", "document"] as const) for (const action of ["start", "stop", "resume", "retry"] as const) {
  for (const scenario of ["single", "continuous", "rejected", "leave"] as const) {
    test(`${kind}/${action}/${scenario} 写回执与状态读取分离`, async ({ page }) => {
      const state = await workspace(page, kind);
      state.latest = action !== "start";
      const originalStatus = action === "stop" ? "running" : action === "retry" ? "failed" : "paused";
      state.task.status = originalStatus;
      await page.clock.install({ time: new Date("2026-10-27T00:00:00Z") });
      await page.clock.pauseAt(new Date("2026-10-27T00:00:01Z"));
      await page.goto("/#summary/file");
      await expect(page.getByRole("heading", { name: "恢复测试", exact: true })).toBeVisible();
      state.failWrite = scenario === "rejected";
      state.failures = scenario === "continuous" ? 2 : scenario === "rejected" ? 0 : 1;
      if (action === "start") {
        await page.getByRole("button", { name: kind === "video" ? "开始处理" : "生成笔记", exact: true }).click();
        const dialog = page.getByRole("dialog", { name: kind === "video" ? "选择处理模型" : "生成文档笔记", exact: true });
        await dialog.getByRole("button", { name: kind === "video" ? "开始处理" : "开始生成", exact: true }).click();
        if (scenario === "rejected") {
          await expect(dialog).toBeVisible();
          await expect(page.getByRole("alert").filter({ hasText: "提交失败，状态未变" })).toBeVisible();
          await dialog.getByRole("button", { name: "取消", exact: true }).click();
        } else await expect(dialog).toHaveCount(0);
        expect(state.bodies).toEqual([kind === "video" ? { asr_option: "paraformer", ocr_option: "rapidocr", llm_profile_id: "default", note_style: "detailed" }
          : { llm_profile_id: "default", note_style: "detailed" }]);
      } else await page.getByRole("button", { name: action === "stop" ? "停止处理" : action === "resume" ? "继续处理" : kind === "video" ? "复用已有识别，重试任务" : "重试任务", exact: true }).click();
      if (scenario === "rejected") {
        if (action !== "start") await expect(page.getByRole("alert").filter({ hasText: "提交失败，状态未变" })).toBeVisible();
        await expect(page.getByText("操作已接受，正在同步状态", { exact: true })).toHaveCount(0);
        expect(state.task.status).toBe(originalStatus);
        await expect(page.getByRole("button", { name: action === "start" ? kind === "video" ? "开始处理" : "生成笔记" : action === "stop" ? "停止处理" : action === "resume" ? "继续处理" : kind === "video" ? "复用已有识别，重试任务" : "重试任务", exact: true })).toBeEnabled();
        const before = state.reads;
        await page.clock.runFor(500);
        await expect.poll(() => state.reads).toBe(action === "stop" ? before + 1 : before);
      } else {
        await expect(page.getByText("操作已接受，正在同步状态", { exact: true })).toBeVisible();
        await expect.poll(() => state.reads).toBe(1);
        if (scenario === "leave") {
          await page.locator(".detail-back").focus();
          await page.keyboard.press("Enter");
          await expect(page.getByRole("region", { name: "文件列表", exact: true })).toBeVisible();
          await page.clock.runFor(11000);
          expect(state.reads).toBe(1);
        } else {
          await page.clock.runFor(5000);
          await expect.poll(() => state.reads).toBe(2);
          if (scenario === "continuous") {
            await expect(page.getByText("操作已接受，正在同步状态", { exact: true })).toBeVisible();
            await page.clock.runFor(5000);
            await expect.poll(() => state.reads).toBe(3);
          }
          await expect(page.getByText("操作已接受，正在同步状态", { exact: true })).toHaveCount(0);
          await expect(page.getByRole("button", { name: action === "stop" ? "继续处理" : "停止处理", exact: true })).toBeEnabled();
        }
      }
      expect(state.writes).toHaveLength(1);
    });
  }
}

for (const kind of ["video", "document"] as const) {
  test(`${kind} 确定任务不存在停止重连，手动刷新只读取`, async ({ page }) => {
    const state = await workspace(page, kind);
    state.missing = true;
    await page.clock.install();
    await page.goto("/#summary/file");
    await page.getByRole("button", { name: "继续处理", exact: true }).click();
    await expect(page.getByRole("button", { name: "重新读取任务", exact: true })).toBeVisible();
    await page.clock.runFor(15000);
    expect(state.reads).toBe(1);
    state.missing = false;
    await page.getByRole("button", { name: "重新读取任务", exact: true }).click();
    await expect(page.getByRole("button", { name: "停止处理", exact: true })).toBeEnabled();
    expect(state.writes).toHaveLength(1);
  });

  test(`${kind} 操作前旧读取迟到不能覆盖确认后的状态`, async ({ page }) => {
    const state = await workspace(page, kind);
    state.task.status = "running";
    let release!: () => void, arrived!: () => void;
    const held = new Promise<void>(resolve => { release = resolve; });
    const started = new Promise<void>(resolve => { arrived = resolve; });
    let finish!: () => void;
    const completed = new Promise<void>(resolve => { finish = resolve; });
    let calls = 0;
    await page.route(`**/api/v1/${kind === "video" ? "tasks" : "document-tasks"}/tracked`, async route => {
      if (++calls !== 1) return route.fallback();
      const old = kind === "video" ? taskResponse({ ...state.task }) : { ...state.task };
      arrived(); await held;
      await route.fulfill({ json: old }).catch(() => {});
      finish();
    });
    await page.clock.install();
    await page.goto("/#summary/file");
    await page.clock.runFor(600); await started;
    await page.getByRole("button", { name: "停止处理", exact: true }).click();
    await expect(page.getByRole("button", { name: "继续处理", exact: true })).toBeEnabled();
    release();
    await completed;
    await expect(page.getByRole("button", { name: "继续处理", exact: true })).toBeEnabled();
    expect(state.writes).toHaveLength(1);
  });
}

for (const initial of ["running", "done"] as const) {
  test(`文档 ${initial} 提取记录仅接管一次且刷新不提交任务`, async ({ page }) => {
    const state = await workspace(page, "document");
    state.latest = false;
    state.task.status = "paused";
    state.extraction = { source_id: "source", document_id: "file", task_id: "tracked", status: initial, parser: "local", progress: {}, error: null };
    await page.clock.install();
    await page.goto("/#summary/file");
    await expect(page.getByRole("heading", { name: "恢复测试", exact: true })).toBeVisible();
    if (initial === "running") {
      await expect(page.getByRole("region", { name: "文档内容预览", exact: true })).toBeVisible();
      expect(state.reads).toBe(0);
      state.extraction.status = "done";
      await page.clock.runFor(1600);
    }
    await expect(page.getByRole("button", { name: "继续处理", exact: true })).toBeEnabled();
    await page.clock.runFor(5000);
    expect(state.reads).toBe(1);
    await page.reload();
    await expect(page.getByRole("button", { name: "继续处理", exact: true })).toBeEnabled();
    expect(state.reads).toBe(2);
    expect(state.writes).toEqual([]);
  });
}

test("历史 done 提取记录不覆盖最新手动任务", async ({ page }) => {
  const state = await workspace(page, "document");
  state.extraction = { source_id: "old-source", document_id: "file", task_id: "old-task", status: "done", parser: "local", progress: {}, error: null };
  await page.clock.install();
  await page.goto("/#summary/file");
  await expect(page.getByRole("button", { name: "继续处理", exact: true })).toBeEnabled();
  await page.getByRole("button", { name: "继续处理", exact: true }).click();
  await expect(page.getByRole("button", { name: "停止处理", exact: true })).toBeEnabled();
  await page.clock.runFor(6000);
  expect(state.writes).toEqual(["/document-tasks/tracked/resume"]);
  await expect(page.getByRole("button", { name: "停止处理", exact: true })).toBeEnabled();
});

test("首次任一列表接口失败可恢复，已有列表失败保留文件", async ({ page }) => {
  const state = await workspace(page, "video");
  let failed = true;
  await page.route("**/api/v1/documents", route => failed
    ? route.fulfill({ status: 503, json: { error: { error_id: "documents", message: "文档列表读取失败" } } }) : route.fallback());
  await page.goto("/#summary");
  const list = page.getByRole("region", { name: "文件列表", exact: true });
  await expect(list.getByRole("button", { name: "重新读取", exact: true })).toBeEnabled();
  await expect(list).not.toContainText("正在读取文件…");
  failed = false;
  await list.getByRole("button", { name: "重新读取", exact: true }).click();
  await expect(list.locator(".file-name")).toHaveText(["恢复测试"]);
  state.task.status = "running";
  await page.reload();
  await expect(list.locator(".file-name")).toHaveText(["恢复测试"]);
  failed = true;
  await expect(list.getByRole("button", { name: "重新读取", exact: true })).toBeEnabled();
  await expect(list.locator(".file-name")).toHaveText(["恢复测试"]);
  failed = false;
  await list.getByRole("button", { name: "重新读取", exact: true }).click();
  await expect(list.getByRole("button", { name: "重新读取", exact: true })).toHaveCount(0);
});

test("新提取去重到已有文档时接管新任务而非列表旧任务", async ({ page }) => {
  const state = await workspace(page, "document");
  state.extraction = { source_id: "file", document_id: null, task_id: "tracked", status: "running", parser: "local", progress: {}, error: null };
  await page.clock.install();
  await page.goto("/#summary/file");
  await expect(page.getByRole("region", { name: "文档提取进度", exact: true })).toBeVisible();
  state.fileId = "existing";
  state.extraction = { ...state.extraction, document_id: "existing", status: "done" };
  state.task.document_id = "existing";
  state.task.status = "running";
  // A stale existing task is returned by the list, while the extraction receipt identifies the new one.
  await page.route("**/api/v1/documents", route => route.fulfill({ json: [{ id: "existing", kind: "document", filename: "已有文档", format: "txt", extracted: true,
    size_bytes: 10, unit_count: 1, character_count: 10, created_at: state.task.created_at, warnings: [], extraction: state.extraction,
    latest_task: { ...state.task, id: "old-task", status: "paused" } }] }));
  await page.clock.runFor(1600);
  await expect(page).toHaveURL(/#summary\/existing$/);
  await expect(page.getByRole("button", { name: "停止处理", exact: true })).toBeEnabled();
  expect(state.reads).toBeGreaterThan(0);
  expect(state.writes).toEqual([]);
});

test("删除后的手动刷新等待旧后台读取，但另发操作后的读取", async ({ page }) => {
  const state = await workspace(page, "video");
  state.task.status = "running";
  await page.clock.install();
  await page.goto("/#summary");
  const list = page.getByRole("region", { name: "文件列表", exact: true });
  await expect(list.locator(".file-name")).toHaveText(["恢复测试"]);
  let release!: () => void, arrived!: () => void, removed = false, reads = 0;
  const held = new Promise<void>(resolve => { release = resolve; });
  const started = new Promise<void>(resolve => { arrived = resolve; });
  await page.route("**/api/v1/videos", async route => {
    reads++;
    if (reads === 1) {
      const json = [{ id: "file", filename: "恢复测试", duration: 120, size_bytes: 10, created_at: state.task.created_at, latest_task: taskResponse(state.task) }];
      arrived(); await held;
      return route.fulfill({ json });
    }
    return removed ? route.fulfill({ json: [] }) : route.fallback();
  });
  await page.route("**/api/v1/videos/file", route => {
    expect(route.request().method()).toBe("DELETE"); removed = true;
    return route.fulfill({ json: {} });
  });
  await page.clock.runFor(2000); await started;
  await list.getByRole("button", { name: "管理文件：恢复测试", exact: true }).click();
  await page.getByRole("button", { name: "删除", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "删除文件？", exact: true });
  await dialog.getByRole("button", { name: "确认删除", exact: true }).click();
  await expect.poll(() => removed).toBe(true);
  await expect(dialog).toBeVisible();
  expect(reads).toBe(1);
  release();
  await expect(dialog).toHaveCount(0);
  await expect(list.locator(".file-name")).toHaveCount(0);
  expect(reads).toBe(2);
});

for (const backgroundKind of ["video", "document"] as const) {
  test(`${backgroundKind} 后台刷新与文档交接同时遇到三秒列表响应仍更新终态`, async ({ page }) => {
    const state = await workspace(page, "document");
    state.latest = false;
    state.extraction = { source_id: "file", document_id: "file", task_id: "tracked", status: "running", parser: "local", progress: {}, error: null };
    const background = { id: "background", kind: backgroundKind, filename: "后台资料", duration: 120, format: "txt", extracted: true,
      unit_count: 1, character_count: 10, size_bytes: 10, created_at: state.task.created_at,
      latest_task: { ...state.task, id: "background-task", video_id: "background", document_id: "background", status: "running" } };
    const reads = { videos: 0, documents: 0, completed: 0, active: 0, maxActive: 0, extraction: 0 };
    await page.route("**/api/v1/documents/file/extraction", route => {
      reads.extraction++;
      return route.fulfill({ json: state.extraction });
    });
    for (const path of ["videos", "documents"] as const) {
      await page.route(`**/api/v1/${path}`, async route => {
        const count = ++reads[path];
        const current = { ...background, latest_task: backgroundKind === "video" ? taskResponse(background.latest_task) : { ...background.latest_task } };
        const json = path === "videos" ? backgroundKind === "video" ? [current] : [] : [
          { id: "file", kind: "document", filename: "恢复测试", format: "txt", extracted: true, size_bytes: 10,
            unit_count: 1, character_count: 10, created_at: state.task.created_at, extraction: state.extraction, latest_task: null },
          ...(backgroundKind === "document" ? [current] : []),
        ];
        if (path === "videos") { reads.active++; reads.maxActive = Math.max(reads.maxActive, reads.active); }
        try {
          if (count > 1) await new Promise(resolve => setTimeout(resolve, 3000));
          await route.fulfill({ json });
          if (path === "videos") reads.completed++;
        } finally { if (path === "videos") reads.active--; }
      });
    }
    try {
      await page.goto("/#summary/file");
      const row = page.locator(".sidebar .library-item").filter({ hasText: "后台资料" });
      await expect(row).toContainText("处理中");
      await expect.poll(() => [reads.videos, reads.documents]).toEqual([2, 2]);
      background.latest_task.status = "succeeded";
      await expect(row).toContainText("已总结", { timeout: 16000 });
      expect(reads.extraction).toBeGreaterThanOrEqual(2);
      expect(reads.completed).toBeGreaterThanOrEqual(3);
      expect(reads.maxActive).toBe(1);
      expect(state.writes).toEqual([]);
    } finally {
      await test.info().attach("refresh-history", { body: JSON.stringify(reads), contentType: "application/json" });
      await page.goto("/#home");
      await page.unrouteAll({ behavior: "wait" });
    }
  });
}
